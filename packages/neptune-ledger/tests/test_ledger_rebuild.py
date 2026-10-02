"""The rebuild-from-packages guarantee (MVL-94; Ledger ADR 0012; docs/guarantees.md).

The acceptance test registers the fixture packages in a shuffled order through the ``ledger``
CLI, dumps the catalog, rebuilds it from the registry manifest alone, dumps it again and compares
the bytes. The rest pins the parts: the manifest after every registration and as hostile input,
the dump's order independence and the columns it leaves out, and a rebuild that refuses, prunes,
continues the clock or builds a new lineage.
"""

import io
import random
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any, Final

import psycopg
import pytest

from conftest import new_database
from neptune.identity import canonical_json
from neptune.model.knowledge import Known
from neptune_ledger.api import CatalogUnavailable
from neptune_ledger.catalog.manifest import (
    FORMAT,
    Manifest,
    ManifestError,
    ManifestNotWritten,
    read_manifest,
)
from neptune_ledger.catalog.migrate import apply_migrations
from neptune_ledger.catalog.rebuild import LEFT_OUT, TRANSACTION_COLUMNS, dump, rebuild
from neptune_ledger.catalog.registry import PostgresCatalog
from neptune_ledger.cli import main
from neptune_ledger.contract_tests.examples import (
    EXAMPLES,
    WorkedPackage,
    materialise,
    reparse,
    with_chunk_size,
    with_moved_source,
    write,
)
from test_ledger_registration import dump as dump_tables

Conn = psycopg.Connection[tuple[object, ...]]
# The four contract examples, the schema-4 lifecycle cell, and three variants of the drone that
# exercise lineage siblings, a moved source (absences) and per-package source chunking.
FIXTURES: Final = (*EXAMPLES, "manipulator_cell")
SEEDS: Final = (94, 2026)


@pytest.fixture
def packages(tmp_path: Path) -> list[WorkedPackage]:
    root = tmp_path / "packages"
    made = [materialise(name, root / name) for name in FIXTURES]
    made.append(write("drone-v2", root / "drone-v2", reparse("drone", "2.0.0", {})))
    moved = with_moved_source("drone", "moved/flight.ulg")
    made.append(write("drone-moved", root / "drone-moved", moved))
    made.append(write("drone-chunks", root / "drone-chunks", with_chunk_size("drone", 4096)))
    return made


def _cli(uri: str, *args: str, tenant: str = "acme") -> int:
    return main(["--dsn", uri, "--tenant", tenant, *args])


def _dump(uri: str, tenant: str = "acme") -> bytes:
    out = io.BytesIO()
    dump(uri, tenant, out)
    return out.getvalue()


def _register_all(uri: str, packages: list[WorkedPackage], tenant: str = "acme") -> None:
    with psycopg.connect(uri, autocommit=True) as conn:
        apply_migrations(conn, tenant)
    with PostgresCatalog(uri, tenant, package_roots=None) as catalog:
        for package in packages:
            assert catalog.register(package.root).outcome == "registered", package.name


# --- the guarantee -----------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize("seed", SEEDS)
def test_a_rebuild_from_the_manifest_and_packages_is_byte_identical(
    pg_server: str,
    pg_uri: str,
    packages: list[WorkedPackage],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    seed: int,
) -> None:
    """Acceptance (MVL-94): register in a shuffled order, dump; rebuild from the manifest, in
    place and into a fresh, empty database, and dump each; the dumps are byte-identical, and so
    is every table, transaction columns included."""
    order = list(packages)
    random.Random(seed).shuffle(order)
    roots = ["--package-root", str(tmp_path / "packages")]
    manifest, again = tmp_path / "manifest.json", tmp_path / "manifest-after.json"
    before, after = tmp_path / "before.jsonl", tmp_path / "after.jsonl"

    assert _cli(pg_uri, "migrate") == 0
    for package in order:
        assert _cli(pg_uri, "--manifest", str(manifest), "register", str(package.root), *roots) == 0
    assert _cli(pg_uri, "dump", "--out", str(before)) == 0
    with psycopg.connect(pg_uri) as conn:
        tables_before = dump_tables(conn, "tenant_acme")
    capsys.readouterr()

    rebuilt = _cli(pg_uri, "--manifest", str(again), "rebuild", "--from", str(manifest), *roots)
    report = canonical_json.loads(capsys.readouterr().out.strip().encode())
    assert rebuilt == 0, report
    assert report == {
        "ledger_version": "0.0.1",
        "outcome": "rebuilt",
        "packages": len(packages),
        "tenant_id": "acme",
    }
    assert _cli(pg_uri, "dump", "--out", str(after)) == 0

    assert after.read_bytes() == before.read_bytes()
    assert again.read_bytes() == manifest.read_bytes()
    with psycopg.connect(pg_uri) as conn:
        assert dump_tables(conn, "tenant_acme") == tables_before
    listed = [e.package_id for e in Manifest.from_bytes(manifest.read_bytes()).registrations]
    assert listed == [package.package_id for package in order]
    assert before.read_bytes().count(b"\n") > 500

    # From the manifest and packages alone: a database that never saw a registration.
    fresh, elsewhere = new_database(pg_server), tmp_path / "fresh.jsonl"
    assert _cli(fresh, "rebuild", "--from", str(manifest), *roots) == 0
    assert _cli(fresh, "dump", "--out", str(elsewhere)) == 0
    assert elsewhere.read_bytes() == before.read_bytes()
    with psycopg.connect(fresh) as conn:
        assert dump_tables(conn, "tenant_acme") == tables_before


@pytest.mark.integration
def test_the_dump_does_not_depend_on_registration_order_or_tenant(
    pg_server: str, packages: list[WorkedPackage]
) -> None:
    """ADR 0009 §4 and ADR 0012 §3: the left-out columns are the only ones order moves."""
    shuffled = list(packages)
    random.Random(SEEDS[0]).shuffle(shuffled)
    by_id = sorted(packages, key=lambda p: p.package_id.encode("utf-8"))
    dumps = []
    for order, tenant in ((shuffled, "acme"), (by_id, "other")):
        uri = new_database(pg_server)
        _register_all(uri, order, tenant)
        dumps.append(_dump(uri, tenant))
    assert dumps[0] == dumps[1]


def test_the_dump_is_canonical_json_lines_with_one_header_per_table(
    pg_uri: str, packages: list[WorkedPackage]
) -> None:
    _register_all(pg_uri, packages[:2])
    lines = _dump(pg_uri).splitlines()
    assert all(canonical_json.dumps(canonical_json.loads(line)) == line for line in lines)
    header = canonical_json.loads(lines[0])
    assert header == {
        "format": "neptune-ledger/catalog-dump",
        "format_version": 1,
        "left_out": sorted(LEFT_OUT),
    }
    headers = [canonical_json.loads(line) for line in lines[1:] if line.startswith(b'{"columns":[')]
    tables = [str(v["table"]) for v in headers if isinstance(v, dict)]
    assert tables == sorted(tables, key=lambda t: t.encode("utf-8"))
    assert {"record", "registration_log", "thread_member", "tx_clock"} <= set(tables)
    assert not any(t.startswith("record_") and t != "record_logical_id" for t in tables)
    columns: set[str] = set()
    for line in lines[1:]:
        value = canonical_json.loads(line)
        assert isinstance(value, dict)
        if "table" in value and isinstance(value.get("columns"), list):
            assert not set(value["columns"]) & LEFT_OUT
            assert value["columns"] == sorted(value["columns"], key=lambda c: c.encode())
            columns = set(value["columns"])
        else:  # a row: column texts, a NULL column absent
            assert set(value) <= columns
            assert all(isinstance(text, str) for text in value.values())


def test_every_transaction_column_is_left_out_of_the_dump(pg: Conn) -> None:
    """A later migration that adds a transaction key or time under a new name fails here."""
    apply_migrations(pg, "acme")
    columns = pg.execute(
        "SELECT table_name, column_name, coalesce(domain_name, data_type)"
        " FROM information_schema.columns WHERE table_schema = 'tenant_acme'"
    ).fetchall()
    names = {str(column) for _, column, _ in columns}
    timed = {
        str(column)
        for _, column, kind in columns
        if kind == "tx_time" or "seq" in str(column) or "registration_key" in str(column)
    }
    assert timed == TRANSACTION_COLUMNS
    assert names >= TRANSACTION_COLUMNS


# --- the registry manifest --------------------------------------------------------------------


def test_the_manifest_is_rewritten_after_every_registration_that_adds_a_package(
    pg_uri: str, packages: list[WorkedPackage], tmp_path: Path
) -> None:
    path = tmp_path / "manifest.json"
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        apply_migrations(conn, "acme")
    with PostgresCatalog(pg_uri, "acme", package_roots=None, manifest=path) as catalog:
        for count, package in enumerate(packages[:3], start=1):
            assert catalog.register(package.root).outcome == "registered"
            manifest = Manifest.from_bytes(path.read_bytes())
            assert [e.package_id for e in manifest.registrations] == [
                p.package_id for p in packages[:count]
            ]
        written = path.read_bytes()
        path.unlink()
        assert catalog.register(tmp_path / "nowhere").outcome == "refused"
        assert not path.exists()  # a refusal writes nothing
        assert catalog.register(packages[0].root).outcome == "already_registered"
        assert path.read_bytes() == written  # registering again repairs a stale manifest
    with psycopg.connect(pg_uri) as conn:
        assert read_manifest(conn, "acme").to_bytes() == written
    entry = Manifest.from_bytes(written).registrations[0]
    assert (entry.tx_seq, entry.root_locator, entry.ledger_version) == (
        1,
        str(packages[0].root),
        "0.0.1",
    )
    assert not list(tmp_path.glob(".manifest.json.*"))  # the temporary file was renamed


def test_a_manifest_that_cannot_be_written_still_reports_the_registration(
    pg_uri: str, packages: list[WorkedPackage], tmp_path: Path
) -> None:
    path = tmp_path / "later" / "manifest.json"
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        apply_migrations(conn, "acme")
    with PostgresCatalog(pg_uri, "acme", package_roots=None, manifest=path) as catalog:
        with pytest.raises(ManifestNotWritten) as caught:
            catalog.register(packages[0].root)
        assert caught.value.registration.outcome == "registered"
        path.parent.mkdir()
        assert catalog.register(packages[0].root).outcome == "already_registered"
    assert len(Manifest.from_bytes(path.read_bytes()).registrations) == 1


def test_ledger_manifest_writes_the_file_or_stdout(
    pg_uri: str, packages: list[WorkedPackage], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _register_all(pg_uri, packages[:2])
    path = tmp_path / "manifest.json"
    assert _cli(pg_uri, "--manifest", str(path), "manifest") == 0
    assert _cli(pg_uri, "manifest") == 0
    assert capsys.readouterr().out.encode() == path.read_bytes()
    assert len(Manifest.from_bytes(path.read_bytes()).registrations) == 2


_ENTRY: Final[dict[str, Any]] = {
    "ledger_version": "0.0.1",
    "package_id": "sha256:" + "a" * 64,
    "root_locator": "/srv/packages/a",
    "tx_seq": 1,
    "tx_time": "2026-10-02T12:00:00.000000Z",
}


def _manifest(*entries: dict[str, Any], **top: Any) -> bytes:
    document = {
        "format": FORMAT,
        "format_version": 1,
        "registrations": list(entries),
        "tenant_id": "acme",
        **top,
    }
    return canonical_json.dumps(document) + b"\n"


def test_a_manifest_round_trips() -> None:
    second = {**_ENTRY, "package_id": "sha256:" + "b" * 64, "tx_seq": 3}
    data = _manifest(_ENTRY, second)
    assert Manifest.from_bytes(data).to_bytes() == data
    assert Manifest.from_bytes(_manifest()).registrations == ()


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(_manifest(_ENTRY)[:-1], id="no-final-newline"),
        pytest.param(b'{"format": 1}\n', id="not-canonical"),
        pytest.param(b"[]\n", id="not-an-object"),
        pytest.param(_manifest(extra=1), id="extra-key"),
        pytest.param(_manifest(format="neptune-ledger/other"), id="other-format"),
        pytest.param(_manifest(format_version=2), id="other-version"),
        pytest.param(_manifest(tenant_id="Acme"), id="bad-tenant"),
        pytest.param(_manifest(tenant_id=7), id="tenant-not-text"),
        pytest.param(_manifest(registrations={}), id="registrations-not-a-list"),
        pytest.param(_manifest({**_ENTRY, "tx_seq": True}), id="seq-bool"),
        pytest.param(_manifest({**_ENTRY, "tx_seq": 0}), id="seq-zero"),
        pytest.param(_manifest({**_ENTRY, "tx_seq": 2**63}), id="seq-too-large"),
        pytest.param(_manifest({**_ENTRY, "tx_time": "2026-10-02T12:00:00Z"}), id="time-shape"),
        pytest.param(_manifest({**_ENTRY, "package_id": "sha256:AB"}), id="package-id"),
        pytest.param(_manifest({**_ENTRY, "root_locator": "packages/a"}), id="relative-root"),
        pytest.param(_manifest({**_ENTRY, "root_locator": "/a\x00b"}), id="nul-in-root"),
        pytest.param(_manifest({**_ENTRY, "ledger_version": "1.0"}), id="version-shape"),
        pytest.param(_manifest({**_ENTRY, "note": "x"}), id="entry-extra-key"),
        pytest.param(
            _manifest(_ENTRY, {**_ENTRY, "package_id": "sha256:" + "b" * 64}), id="seq-repeats"
        ),
        pytest.param(
            _manifest(
                _ENTRY,
                {
                    **_ENTRY,
                    "package_id": "sha256:" + "b" * 64,
                    "tx_seq": 2,
                    "tx_time": "2026-10-02T11:59:59.999999Z",
                },
            ),
            id="time-goes-back",
        ),
        pytest.param(_manifest(_ENTRY, {**_ENTRY, "tx_seq": 2}), id="package-twice"),
    ],
)
def test_a_malformed_manifest_is_refused(data: bytes) -> None:
    with pytest.raises(ManifestError):
        Manifest.from_bytes(data)


# --- rebuild -----------------------------------------------------------------------------------


@pytest.fixture
def registered(pg_uri: str, packages: list[WorkedPackage]) -> Manifest:
    _register_all(pg_uri, packages[:4])
    with psycopg.connect(pg_uri) as conn:  # closed before the test: a rebuild waits on readers
        return read_manifest(conn, "acme")


def test_a_refused_package_rolls_the_whole_rebuild_back(
    pg_uri: str, registered: Manifest, packages: list[WorkedPackage]
) -> None:
    before = _dump(pg_uri)
    (packages[2].root / "records" / "run.jsonl").write_bytes(b"")
    report = rebuild(pg_uri, registered, package_roots=None)
    assert report.outcome == "refused"
    assert report.failed == registered.registrations[2]
    assert report.registration is not None
    assert [f.code for f in report.registration.findings] == ["file_digest_mismatch"]
    assert _dump(pg_uri) == before
    with psycopg.connect(pg_uri) as conn:
        assert read_manifest(conn, "acme") == registered


def test_another_package_at_a_logged_root_is_refused(
    pg_uri: str, registered: Manifest, packages: list[WorkedPackage]
) -> None:
    """An intact package that is not the logged one must not take its tick in the rebuilt log."""
    before = _dump(pg_uri)
    first = registered.registrations[0]
    shutil.rmtree(first.root_locator)
    shutil.copytree(packages[4].root, first.root_locator)
    report = rebuild(pg_uri, registered, package_roots=None)
    assert (report.outcome, report.failed) == ("refused", first)
    assert report.registration is not None
    assert report.registration.package_id == Known(packages[4].package_id)
    assert [(f.code, f.subject) for f in report.registration.findings] == [
        ("manifest_digest_mismatch", "manifest.json")
    ]
    assert first.package_id in report.registration.findings[0].detail
    assert _dump(pg_uri) == before
    with psycopg.connect(pg_uri) as conn:
        assert read_manifest(conn, "acme") == registered


def test_a_root_outside_the_package_roots_is_refused(
    pg_uri: str, registered: Manifest, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    report = rebuild(pg_uri, registered, package_roots=[str(elsewhere)])
    assert report.outcome == "refused"
    assert report.registration is not None
    assert [f.code for f in report.registration.findings] == ["package_unreadable"]


def test_a_logged_root_that_now_resolves_elsewhere_is_refused(
    pg_uri: str, registered: Manifest, tmp_path: Path
) -> None:
    """The rebuilt log would record another root, so the rebuild says so instead."""
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "packages", target_is_directory=True)
    first = registered.registrations[0]
    moved = Path(first.root_locator).relative_to(tmp_path / "packages")
    through = replace(first, root_locator=str(link / moved))
    manifest = Manifest("acme", (through, *registered.registrations[1:]))
    report = rebuild(pg_uri, manifest, package_roots=None)
    assert (report.outcome, report.failed) == ("refused", through)
    assert report.registration is not None
    assert [f.code for f in report.registration.findings] == ["package_unreadable"]


def test_a_stale_manifest_does_not_drop_packages_unless_pruned(
    pg_uri: str, registered: Manifest
) -> None:
    stale = Manifest("acme", registered.registrations[:3])
    report = rebuild(pg_uri, stale, package_roots=None)
    assert (report.outcome, report.unlisted) == (
        "refused",
        (registered.registrations[3].package_id,),
    )
    with psycopg.connect(pg_uri) as conn:
        assert read_manifest(conn, "acme") == registered
    assert rebuild(pg_uri, stale, package_roots=None, prune=True).outcome == "rebuilt"
    with psycopg.connect(pg_uri) as conn:
        assert read_manifest(conn, "acme") == stale


def test_live_registration_continues_after_the_replayed_ticks(
    pg_uri: str, registered: Manifest, packages: list[WorkedPackage]
) -> None:
    assert rebuild(pg_uri, registered, package_roots=None).outcome == "rebuilt"
    with PostgresCatalog(pg_uri, "acme", package_roots=None) as catalog:
        answer = catalog.register(packages[4].root)
    key = answer.registration_key
    assert answer.outcome == "registered" and isinstance(key, Known)
    assert key.value.tx_seq == 5
    assert key.value.tx_time >= registered.registrations[-1].tx_time


def test_another_ledger_version_rebuilds_a_new_lineage_over_the_same_keys(
    pg_uri: str, registered: Manifest
) -> None:
    refused = rebuild(pg_uri, registered, package_roots=None, ledger_version="0.0.2")
    assert (refused.outcome, refused.other_versions) == ("refused", ("0.0.1",))
    report = rebuild(
        pg_uri, registered, package_roots=None, ledger_version="0.0.2", new_lineage=True
    )
    assert (report.outcome, report.ledger_version) == ("rebuilt", "0.0.2")
    with psycopg.connect(pg_uri) as conn:
        now = read_manifest(conn, "acme")
    assert [(e.tx_seq, e.tx_time, e.package_id, e.root_locator) for e in now.registrations] == [
        (e.tx_seq, e.tx_time, e.package_id, e.root_locator) for e in registered.registrations
    ]
    assert {e.ledger_version for e in now.registrations} == {"0.0.2"}


def test_a_rebuild_creates_a_tenant_that_does_not_exist_yet(
    pg_server: str, registered: Manifest
) -> None:
    uri = new_database(pg_server)
    assert rebuild(uri, registered, package_roots=None).outcome == "rebuilt"
    with psycopg.connect(uri) as conn:
        assert read_manifest(conn, "acme") == registered
    empty = Manifest("fresh", ())
    assert rebuild(uri, empty, package_roots=None).outcome == "rebuilt"
    assert _dump(uri, "fresh").count(b"\n") > 10  # the schema and its seed rows


def test_the_cli_refuses_another_tenants_manifest_and_a_rebuild_without_roots(
    pg_uri: str, registered: Manifest, tmp_path: Path
) -> None:
    path = tmp_path / "manifest.json"
    path.write_bytes(registered.to_bytes())
    roots = ["--package-root", str(tmp_path)]
    assert _cli(pg_uri, "rebuild", "--from", str(path), *roots, tenant="other") == 2
    assert _cli(pg_uri, "rebuild", "--from", str(path)) == 2
    path.write_bytes(b"{}\n")
    assert _cli(pg_uri, "rebuild", "--from", str(path), *roots) == 2
    assert _cli(pg_uri, "rebuild", "--from", str(tmp_path / "missing.json"), *roots) == 2


def test_dumping_a_tenant_without_a_catalog_is_unavailable(pg_uri: str, tmp_path: Path) -> None:
    with pytest.raises(CatalogUnavailable):
        _dump(pg_uri, "nobody")
    out = tmp_path / "dump.jsonl"
    out.write_bytes(b"an earlier dump\n")
    assert _cli(pg_uri, "dump", "--out", str(out), tenant="nobody") == 2
    assert out.read_bytes() == b"an earlier dump\n"  # replaced only by a complete dump
    assert [p.name for p in tmp_path.iterdir()] == ["dump.jsonl"]
