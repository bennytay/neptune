"""``PostgresCatalog.register`` and ``verify`` beyond the contract suite (MVL-90).

Determinism, concurrency, refusals that write nothing, hostile packages, tenant roots, ``as_of``,
retries, and agreement with the walkthrough's column mapping. The contract tests themselves run in
``tests/contract/test_ledger_catalog_contract.py``.
"""

import multiprocessing
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from conftest import new_database
from neptune.identity import canonical_json
from neptune.model.knowledge import Known, NotApplicable, NotCovered, Unknown
from neptune_ledger.api import CatalogUnavailable, codec
from neptune_ledger.catalog.migrate import apply_migrations
from neptune_ledger.catalog.registry import PostgresCatalog
from neptune_ledger.contract_tests.examples import EXAMPLES, WorkedPackage, materialise
from test_catalog_walkthrough import load_package
from test_catalog_walkthrough import register as harness_register

Conn = psycopg.Connection[tuple[object, ...]]
TX_COLUMNS = ("tx_time", "last_time")


def fresh(uri: str, tenant: str = "acme", roots: Any = None) -> PostgresCatalog:
    with psycopg.connect(uri, autocommit=True) as conn:
        apply_migrations(conn, tenant)
    return PostgresCatalog(uri, tenant, package_roots=roots)


@pytest.fixture
def catalog(pg_uri: str) -> Iterator[PostgresCatalog]:
    with fresh(pg_uri) as made:
        yield made


@pytest.fixture
def drone(tmp_path: Path) -> WorkedPackage:
    return materialise("drone", tmp_path / "packages" / "drone")


def dump(conn: Conn, schema: str, skip: tuple[str, ...] = ()) -> dict[str, list[str]]:
    """Every row of every table in ``schema`` as text, sorted; ``skip`` names columns left out."""
    tables = [
        str(row[0])
        for row in conn.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = %s AND c.relkind IN ('r', 'p') AND NOT c.relispartition",
            (schema,),
        ).fetchall()
    ]
    out: dict[str, list[str]] = {}
    for table in sorted(tables):
        columns = [
            str(row[0])
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
                (schema, table),
            ).fetchall()
            if row[0] not in (*skip, "tenant_id")
        ]
        rows = conn.execute(
            f"SELECT ROW({', '.join(columns)})::text FROM {schema}.{table} ORDER BY 1"
        ).fetchall()
        out[table] = [str(row[0]) for row in rows]
    return out


def counts(conn: Conn, schema: str = "tenant_acme") -> dict[str, int]:
    return {table: len(rows) for table, rows in dump(conn, schema).items()}


# --- determinism -------------------------------------------------------------------------------


def test_the_same_packages_in_two_empty_ledgers_give_identical_tables(
    pg_server: str, tmp_path: Path
) -> None:
    """Acceptance: registering the same packages into two empty Ledgers gives identical tables.

    Transaction times are the host clock read at registration (ADR 0002 §4) and are the only
    columns left out; the registration log replays them on a rebuild.
    """
    packages = [materialise(name, tmp_path / name) for name in EXAMPLES]
    dumps = []
    for _ in range(2):
        uri = new_database(pg_server)
        with fresh(uri) as catalog:
            for package in packages:
                assert catalog.register(package.root).outcome == "registered"
        with psycopg.connect(uri) as conn:
            dumps.append(dump(conn, "tenant_acme", TX_COLUMNS))
    assert dumps[0] == dumps[1]
    assert sum(len(rows) for rows in dumps[0].values()) > 100
    assert {"registration_log", "package", "record", "record_logical_id"} <= set(dumps[0])


def test_registration_writes_exactly_the_walkthrough_rows(
    pg_uri: str, pg: Conn, tmp_path: Path
) -> None:
    """The real registration and the walkthrough's ADR 0002 §5 harness write the same rows."""
    with fresh(pg_uri) as catalog:
        for name in ("drone", "quadruped", "manipulator", "mobile_robot"):
            assert catalog.register(materialise(name, tmp_path / name).root).outcome == "registered"
    apply_migrations(pg, "harness")
    for name in ("drone", "quadruped", "manipulator", "mobile_robot"):
        harness_register(pg, "tenant_harness", load_package(name))
    skip = (*TX_COLUMNS, "root_locator")
    assert dump(pg, "tenant_acme", skip) == dump(pg, "tenant_harness", skip)


def _register_in_a_process(uri: str, root: str, barrier: Any) -> tuple[str, str, str]:
    with PostgresCatalog(uri, "acme", package_roots=None) as catalog:
        barrier.wait()
        result = catalog.register(root)
    return result.outcome, repr(result.package_id), repr(result.registration_key)


def test_two_processes_registering_one_package_get_one_registration(
    pg_uri: str, pg: Conn, tmp_path: Path
) -> None:
    """One process registers, the other finds it registered; both return the same id and key."""
    fresh(pg_uri).close()
    context = multiprocessing.get_context("spawn")
    for name in EXAMPLES:
        root = str(materialise(name, tmp_path / name).root)
        with context.Manager() as manager:
            barrier = manager.Barrier(2)
            with context.Pool(2) as pool:
                results = pool.starmap(_register_in_a_process, [(pg_uri, root, barrier)] * 2)
        assert sorted(outcome for outcome, _, _ in results) == ["already_registered", "registered"]
        assert results[0][1:] == results[1][1:]
    assert counts(pg)["package"] == len(EXAMPLES)
    assert counts(pg)["registration_log"] == len(EXAMPLES)


# --- refusals write nothing --------------------------------------------------------------------


def test_a_refused_package_writes_nothing_and_allocates_no_tick(
    catalog: PostgresCatalog, pg: Conn, drone: WorkedPackage
) -> None:
    table = drone.root / "records" / "run.jsonl"
    table.write_bytes(table.read_bytes() + b"\n")
    assert catalog.register(drone.root).outcome == "refused"
    assert all(
        n == 0 for t, n in counts(pg).items() if t not in ("tenant", "schema_migration", "tx_clock")
    )
    row = pg.execute("SELECT last_seq FROM tenant_acme.tx_clock").fetchone()
    assert row == (0,)


def test_a_conflict_is_reported_before_anything_is_written(
    catalog: PostgresCatalog, pg: Conn, drone: WorkedPackage, tmp_path: Path
) -> None:
    from neptune_ledger.contract_tests.examples import with_source_size, write

    assert catalog.register(drone.root).outcome == "registered"
    before = dump(pg, "tenant_acme")
    (source,) = drone.records("source_artifact")
    liar = write("liar", tmp_path / "liar", with_source_size("drone", source["size"] + 1))
    result = catalog.register(liar.root)
    assert result.outcome == "refused"
    assert [(f.code, f.subject) for f in result.findings] == [
        ("conflicting_id", source["content_id"])
    ]
    assert dump(pg, "tenant_acme") == before


# --- hostile packages --------------------------------------------------------------------------


def _codes(result: Any) -> list[tuple[str, str]]:
    return [(f.code, f.subject) for f in result.findings]


def _rewrite_manifest(package: WorkedPackage, change: Any) -> None:
    manifest = dict(package.manifest)
    change(manifest)
    (package.root / "manifest.json").write_bytes(canonical_json.dumps(manifest))


def test_a_fifo_in_a_package_is_an_unsafe_entry_and_never_opened(
    catalog: PostgresCatalog, drone: WorkedPackage
) -> None:
    os.mkfifo(drone.root / "records" / "pipe")  # opening it to read would block forever
    result = catalog.register(drone.root)
    assert result.outcome == "refused"
    assert _codes(result) == [("unsafe_entry", "records/pipe")]


def test_a_symlink_in_volatile_is_an_unsafe_entry(
    catalog: PostgresCatalog, drone: WorkedPackage, tmp_path: Path
) -> None:
    (drone.root / "volatile").mkdir()
    (drone.root / "volatile" / "receipt-envelope.json").symlink_to(tmp_path)
    assert _codes(catalog.register(drone.root)) == [
        ("unsafe_entry", "volatile/receipt-envelope.json")
    ]


def test_a_symlinked_root_is_unreadable(
    catalog: PostgresCatalog, drone: WorkedPackage, tmp_path: Path
) -> None:
    link = tmp_path / "link"
    link.symlink_to(drone.root, target_is_directory=True)
    result = catalog.register(link)
    assert result.outcome == "refused"
    assert isinstance(result.package_id, Unknown)
    assert _codes(result) == [("package_unreadable", str(link))]


def test_a_root_without_a_manifest_is_unreadable(
    catalog: PostgresCatalog, drone: WorkedPackage
) -> None:
    (drone.root / "manifest.json").unlink()
    result = catalog.register(drone.root)
    assert _codes(result) == [("package_unreadable", "manifest.json")]
    assert isinstance(result.package_id, Unknown)


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"not json", "manifest_invalid"),
        (b"[]", "manifest_invalid"),
        (b'{"kind":"package_manifest"}', "manifest_invalid"),
    ],
)
def test_a_manifest_that_is_not_one_is_invalid(
    catalog: PostgresCatalog, drone: WorkedPackage, data: bytes, code: str
) -> None:
    (drone.root / "manifest.json").write_bytes(data)
    result = catalog.register(drone.root)
    assert result.outcome == "refused"
    assert [f.code for f in result.findings] == [code]
    assert result.package_id == Known("sha256:" + __import__("hashlib").sha256(data).hexdigest())


def test_another_schema_version_is_unsupported(
    catalog: PostgresCatalog, drone: WorkedPackage
) -> None:
    _rewrite_manifest(drone, lambda m: m.update(schema_version=2))
    result = catalog.register(drone.root)
    assert [f.code for f in result.findings] == ["unsupported_schema_version"]
    assert result.schema_version == Known(2)


def test_an_unlisted_file_is_unexpected(catalog: PostgresCatalog, drone: WorkedPackage) -> None:
    (drone.root / "records" / "extra.jsonl").write_bytes(b"")
    assert _codes(catalog.register(drone.root)) == [("unexpected_file", "records/extra.jsonl")]


@pytest.mark.parametrize("escape", ["../outside.jsonl", "/etc/hostname"])
def test_a_listed_path_outside_the_root_is_missing_and_never_read(
    catalog: PostgresCatalog, drone: WorkedPackage, tmp_path: Path, escape: str
) -> None:
    outside = tmp_path / "packages" / "outside.jsonl"
    outside.write_bytes(b"")

    def add(manifest: dict[str, Any]) -> None:
        empty = "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        manifest["files"] = [*manifest["files"], {"path": escape, "sha256": empty, "size": 0}]

    _rewrite_manifest(drone, add)
    assert _codes(catalog.register(drone.root)) == [("file_missing", escape)]


def test_a_table_with_a_listed_but_invalid_record_is_record_invalid(
    catalog: PostgresCatalog, drone: WorkedPackage
) -> None:
    path = "records/video.jsonl"
    data = b'{"kind":"video"}\n'
    (drone.root / path).write_bytes(data)
    import hashlib

    def relist(manifest: dict[str, Any]) -> None:
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        manifest["files"] = [
            {"path": path, "sha256": digest, "size": len(data)} if f["path"] == path else f
            for f in manifest["files"]
        ]

    _rewrite_manifest(drone, relist)
    result = catalog.register(drone.root)
    assert result.outcome == "refused"
    assert [f.code for f in result.findings] == ["record_invalid"]


def test_every_file_problem_is_reported_together(
    catalog: PostgresCatalog, drone: WorkedPackage
) -> None:
    (drone.root / "records" / "video.jsonl").unlink()
    (drone.root / "records" / "extra.jsonl").write_bytes(b"x")
    run = drone.root / "records" / "run.jsonl"
    run.write_bytes(run.read_bytes() + b" ")
    assert _codes(catalog.register(drone.root)) == [
        ("file_missing", "records/video.jsonl"),
        ("unexpected_file", "records/extra.jsonl"),
        ("file_digest_mismatch", "records/run.jsonl"),
    ]


# --- tenant package roots (ADR 0006 §3) --------------------------------------------------------


def test_a_root_sharing_a_string_prefix_is_outside(pg_uri: str, tmp_path: Path) -> None:
    ours = tmp_path / "b"
    evil = materialise("drone", tmp_path / "b-evil" / "drone")
    good = materialise("drone", ours / "drone")
    with fresh(pg_uri, roots=[ours]) as catalog:
        refused = catalog.register(evil.root)
        assert refused.outcome == "refused"
        assert _codes(refused) == [("package_unreadable", str(evil.root))]
        assert catalog.register(good.root).outcome == "registered"


def test_a_relative_root_resolves_before_containment(pg_uri: str, tmp_path: Path) -> None:
    ours = tmp_path / "b"
    package = materialise("drone", ours / "drone")
    cwd = Path.cwd()
    os.chdir(ours)
    try:
        with fresh(pg_uri, roots=[ours]) as catalog:
            result = catalog.register("drone")
    finally:
        os.chdir(cwd)
    assert result.outcome == "registered"
    assert result.root_locator == str(package.root.resolve())


# --- verify ------------------------------------------------------------------------------------


def test_verify_at_an_earlier_point(catalog: PostgresCatalog, tmp_path: Path) -> None:
    drone = materialise("drone", tmp_path / "drone")
    quadruped = materialise("quadruped", tmp_path / "quadruped")
    first = catalog.register(drone.root)
    catalog.register(quadruped.root)
    assert isinstance(first.registration_key, Known)
    point = first.registration_key.value
    early = catalog.verify(drone.package_id, as_of=point.tx_seq)
    assert (early.verdict, early.as_of) == ("intact", Known(point))
    later = catalog.verify(quadruped.package_id, as_of=point.tx_seq)
    assert later.verdict == "unknown_package"
    assert later.as_of == Known(point)
    assert codec.dumps(catalog.verify(drone.package_id)) == codec.dumps(
        catalog.verify(drone.package_id)
    )


@pytest.mark.parametrize(("as_of", "code"), [(0, "invalid_request"), (99, "as_of_out_of_range")])
def test_verify_rejects_an_as_of_outside_the_catalog(
    catalog: PostgresCatalog, drone: WorkedPackage, as_of: int, code: str
) -> None:
    first = catalog.register(drone.root)
    report = catalog.verify(drone.package_id, as_of=as_of)
    assert [f.code for f in report.findings] == [code]
    assert report.verdict == "unknown_package"
    assert report.registration_key == NotCovered()
    assert report.as_of == first.registration_key


def test_verify_on_an_empty_catalog_has_no_point(catalog: PostgresCatalog) -> None:
    report = catalog.verify("sha256:" + "0" * 64)
    assert report.as_of == NotCovered()
    assert report.verdict == "unknown_package"


def test_verify_reports_a_deleted_table_and_a_new_symlink(
    catalog: PostgresCatalog, drone: WorkedPackage, tmp_path: Path
) -> None:
    catalog.register(drone.root)
    (drone.root / "records" / "video.jsonl").unlink()
    report = catalog.verify(drone.package_id)
    assert (report.verdict, _codes(report)) == (
        "damaged",
        [("file_missing", "records/video.jsonl")],
    )
    assert report.files_checked == drone.listed_files()
    (drone.root / "records" / "video.jsonl").symlink_to(tmp_path / "nowhere")
    report = catalog.verify(drone.package_id)
    assert (report.verdict, _codes(report)) == (
        "damaged",
        [("unsafe_entry", "records/video.jsonl")],
    )


def test_verify_never_follows_a_link_put_on_the_stored_root(
    catalog: PostgresCatalog, tmp_path: Path
) -> None:
    package = materialise("drone", tmp_path / "real" / "drone")
    first = catalog.register(package.root)
    (tmp_path / "real").rename(tmp_path / "elsewhere")
    (tmp_path / "real").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    report = catalog.verify(package.package_id)
    assert report.verdict == "unreachable"
    assert _codes(report) == [("package_unreadable", first.root_locator)]
    assert report.files_checked == 0


# --- the store ---------------------------------------------------------------------------------


def test_a_serialisation_failure_is_retried(catalog: PostgresCatalog) -> None:
    calls = []

    def body(conn: Conn) -> str:
        calls.append(1)
        if len(calls) == 1:
            raise psycopg.errors.SerializationFailure("could not serialize access")
        return "done"

    assert catalog._run(body) == "done"
    assert len(calls) == 2


def test_retries_running_out_is_unavailable(catalog: PostgresCatalog) -> None:
    def body(conn: Conn) -> None:
        raise psycopg.errors.DeadlockDetected("deadlock detected")

    with pytest.raises(CatalogUnavailable, match="kept failing"):
        catalog._run(body)


def test_an_unreachable_store_is_unavailable(drone: WorkedPackage) -> None:
    catalog = PostgresCatalog(
        "postgresql://nobody@127.0.0.1:1/none?connect_timeout=1", "acme", package_roots=None
    )
    with pytest.raises(CatalogUnavailable, match="unreachable"):
        catalog.register(drone.root)
    with pytest.raises(CatalogUnavailable):
        catalog.verify(drone.package_id)


def test_a_registration_reports_the_stored_root_after_a_move(
    catalog: PostgresCatalog, drone: WorkedPackage, tmp_path: Path
) -> None:
    first = catalog.register(drone.root)
    moved = tmp_path / "moved"
    shutil.move(drone.root, moved)
    again = catalog.register(moved)
    assert again.outcome == "already_registered"
    assert (again.root_locator, again.registration_key) == (
        first.root_locator,
        first.registration_key,
    )
    assert again.record_counts == first.record_counts
    assert first.registration_key != NotApplicable()
